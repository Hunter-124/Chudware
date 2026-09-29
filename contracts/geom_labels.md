# chudware.geomlabel/1 — canonical per-face geometry labels (ViewNet)

Status: **FROZEN** (v1). Append-only; a breaking change is `chudware.geomlabel/2`, never an
in-place edit of v1 semantics.

Executable reference: `training/viewpoint/datasets/importers.py` (code is normative for
behaviour, this file for wording); producer CLI `training/viewpoint/datasets/build_manifest.py`;
source survey and licences `training/viewpoint/datasets/DATASETS.md`.

Purpose: ground truth for generating geometry questions (count, presence/side, through/blind,
type, dimension ratios) about parts the ViewNet utility model is trained on. Labels live on the
faces of the **Chudware engine's** import of a STEP file, never on a source dataset's own face
enumeration.

---

## 1. Storage

- One JSONL record per part, sorted by `part_id`, UTF-8, no trailing commas.
- Default location `$CHUDWARE_VIEWNET_DATA/manifests/geomlabel.jsonl`
  (`$CHUDWARE_VIEWNET_DATA` defaults to `~/.cache/chudware/viewnet`). Manifests and the data they
  point at are never committed.
- Siblings written by the producer: `geomlabel.summary.json` (schema
  `chudware.geomlabel.summary/1`: per-source attempted/kept/dropped with drop reasons, match
  rate mean/min, CANON face and feature histograms, split counts) and `geomlabel.reports.jsonl`
  (one match report per attempted part: `part_id`, `n_source_faces`, `n_engine_faces`,
  `match_rate`, `unmatched_source` source-face indices, `unmatched_engine` ordinals, `dropped`
  reason or null).

## 2. Record

```
{"schema": "chudware.geomlabel/1",
 "part_id": str,                 // "<dataset>/<original_id>"; GSL: "gsl/<family>/<seed>"
 "split": "train"|"val"|"test",  // §4
 "source": {"dataset": str, "license": str, "url": str, "original_id": str},
 "file": str,                    // absolute path of the STEP in the local cache
 "units": "mm",
 "faces": [{"ordinal": int, "body": int, "face_id": int,
            "class": CANON, "source_class": str, "instance": int}],
 "features": [{"instance": int, "class": CANON, "faces": [ordinal, ...],
               "through": bool|null, "opening_side": "+X"|"-X"|"+Y"|"-Y"|"+Z"|"-Z"|null,
               "centroid": [x, y, z], "axis": [x, y, z]|null}],
 "stats": {"n_faces": int, "n_bodies": int, "bbox": {"min": [3], "max": [3]},
           "match_rate": float, "n_unmatched": int}}
```

- `faces` lists EVERY face of the engine import, in `ordinal` order.
- `ordinal` — index into `doc_query({"of":"face"})` matches of the document produced by
  `import_file {"path": file, "format": "step"}` (document-wide running order, 0-based).
  Consumers MUST open the part exactly that way; no other import parameters.
- `body` — 1-based body ordinal (= the `body` stamp on the doc_query match = the owner_map /
  AICM body ordinal). `face_id` — per-body 1-based TopExp face id (= the match's `topo` stamp =
  AICM `face_id`). Ordinals are document-wide, face ids are per body; join AICM/metrics faces on
  `(body, face_id)`, never on `face_id` alone.
- `class` — CANON (§3). `source_class` — the source dataset's own label string for the matched
  source face, or `"unmatched"` for an engine face no source face matched (its `class` is then
  `other` and its `instance` is -1).
- `instance` — index into `features`, or -1 (stock faces, unmatched faces).
- `features[i].instance == i`; `faces` sorted ascending; features ordered by their first face.
- `centroid` — area-weighted mean of the member faces' centroids (mm).
- `stats.match_rate` — matched pairs / max(#source faces, #engine faces) (§5).
  `stats.n_unmatched` — engine faces without a source match (≤ 2 % of faces by §5).

## 3. CANON taxonomy

`hole_through, hole_blind, passage, pocket, slot_through, slot_blind, step_through, step_blind,
o_ring, boss, rib, fillet, chamfer, stock, extrude_side, extrude_end, cut_side, cut_end,
revolve_side, revolve_end, other`

Families (grouping key and feature-class rule, §6): `hole_*` → hole, `slot_*` → slot, `step_*` →
step, `extrude_*` → extrude, `cut_*` → cut, `revolve_*` → revolve; every other class is its own
family. `stock` never forms a feature.

### 3.1 MFCAD++ and MFInstSeg (shared 25-class index table)

Index order is `feat_names` in `Utils/parameters.py` of both generators. MFCAD++ stores the index
as each STEP `ADVANCED_FACE` name; MFInstSeg stores `seg: {face index: class index}` in
`labels/<id>.json`, face index = TopExp_Explorer order of unique faces of the read STEP.

| idx | source class | CANON |
|---|---|---|
| 0 | chamfer | chamfer |
| 1 | through_hole | hole_through |
| 2 | triangular_passage | passage |
| 3 | rectangular_passage | passage |
| 4 | 6sides_passage | passage |
| 5 | triangular_through_slot | slot_through |
| 6 | rectangular_through_slot | slot_through |
| 7 | circular_through_slot | slot_through |
| 8 | rectangular_through_step | step_through |
| 9 | 2sides_through_step | step_through |
| 10 | slanted_through_step | step_through |
| 11 | Oring | o_ring |
| 12 | blind_hole | hole_blind |
| 13 | triangular_pocket | pocket |
| 14 | rectangular_pocket | pocket |
| 15 | 6sides_pocket | pocket |
| 16 | circular_end_pocket | pocket |
| 17 | rectangular_blind_slot | slot_blind |
| 18 | v_circular_end_blind_slot | slot_blind |
| 19 | h_circular_end_blind_slot | slot_blind |
| 20 | triangular_blind_step | step_blind |
| 21 | circular_blind_step | step_blind |
| 22 | rectangular_blind_step | step_blind |
| 23 | round | fillet |
| 24 | stock | stock |

An index outside 0–24 or an unnamed MFCAD++ face drops the part (`LabelError`).

### 3.2 Fusion 360 Gallery segmentation (non-commercial; research only)

`.seg` = one index per face (TopExp_Explorer order), names from `segment_names.json`.

| source class | CANON |
|---|---|
| ExtrudeSide | extrude_side |
| ExtrudeEnd | extrude_end |
| CutSide | cut_side |
| CutEnd | cut_end |
| Fillet | fillet |
| Chamfer | chamfer |
| RevolveSide | revolve_side |
| RevolveEnd | revolve_end |

### 3.3 GSL (engine-generated, `tools/gsl_dataset_gen.py` families)

Source class from the owning feature (doc_tree `owner_map`, joined on the doc_query
`body`/`topo` stamps):

| source class | rule | CANON |
|---|---|---|
| stock | first body-creating primitive (box/cylinder/sphere/cone/torus/wedge) | stock |
| boss | any later additive primitive | boss |
| hole | bolt_circle / hole / hole_wizard / cbore / csk / counterbore / countersink / tapped_hole / hole_pattern | hole_through or hole_blind, measured per instance (§6) |
| extrude.boss/side, extrude.boss/end | extrude, mode ≠ cut; end = planar face whose normal is parallel (\|n·d\| > 0.95) to the profile sketch normal d | extrude_side, extrude_end |
| extrude.cut/side, extrude.cut/end | extrude, mode = cut; same side/end rule | cut_side, cut_end |
| revolve/side, revolve/end | revolve; end = planar face whose normal is perpendicular (\|n·a\| < 0.05) to the datum revolve axis a | revolve_side, revolve_end |
| rib, fillet, chamfer | same-named feature | rib, fillet, chamfer |
| shell | faces created by a shell (the hollowed cavity) | pocket |
| anything else (sweep, loft, `import_or_base`, a pattern whose seed cannot be classified, an extrude with no resolvable sketch normal) | — | other |

A circular/linear pattern copy takes its seed feature's class; a circular-pattern copy of an
extrude keeps side/end only when the pattern axis is parallel to the seed's extrude direction,
otherwise it is `other`.

## 4. Split

`b = int(sha1(part_id as UTF-8).hexdigest(), 16) mod 100`; `b < 80` → train, `b < 90` → val,
else test. Whole parts only; the same `part_id` always lands in the same split.

## 5. Face correspondence (normative)

1. Source faces are enumerated independently of the engine: STEP sources through OCP
   (`STEPControl_Reader`, unique faces in TopExp_Explorer order — the order the source tooling
   indexes labels in); GSL faces from the generating engine document's doc_query matches.
   Each carries `surf` (plane|cylinder|cone|sphere|torus|bspline|other, the engine's
   GeomAbs mapping), area (mm²) and centroid (mm).
2. The STEP is imported with the engine (`import_file`, format step) and enumerated with
   `doc_query({"of":"face"})`.
3. A candidate pair needs the same `surf`, centroid distance ≤ max(1e-3 × bbox diagonal,
   1e-3 mm) and |Δarea| ≤ 1e-2 × max(area) + 1e-4 mm². Pairs are accepted one-to-one in
   ascending cost `d/pos_tol + (Δarea/max area)/1e-2`, ties by (source index, ordinal).
   Enumeration order never participates.
4. `match_rate = pairs / max(#source, #engine)`. A part with `match_rate < 0.98` is dropped and
   reported (`dropped: "match_rate …"`); a kept part's unmatched engine faces are
   `class: other, source_class: "unmatched"`.

## 6. Instances and feature geometry

- Adjacency: two faces of the same body are adjacent when they share ≥ 2 vertices of the engine
  tessellation (a common B-rep edge). Covers every body (doc_adjacency covers only the last).
- Grouping: a dataset's own instances when it has them (MFInstSeg `inst` matrix, mapped through
  the correspondence); every remaining matched non-stock face is grouped by
  (owning feature for GSL else none, family) into connected components over the adjacency.
- Feature class: `extrude_side` / `cut_side` / `revolve_side` for those families; otherwise the
  member class with the largest total area. GSL `hole` instances become `hole_through` when the
  instance is geometrically through (below), else `hole_blind` — faces and feature alike.
- `net` = Σ area × outward normal over the instance's planar faces (removal faces point into the
  void, so `net` equals the openings' vector area). `net` is significant when
  |net| > 0.05 × planar area.
- `through`: true for hole_through / slot_through / step_through / passage; false for
  hole_blind / slot_blind / step_blind / pocket; for cut_side the geometric test — the instance
  borders two non-member faces whose normals satisfy n₁·n₂ < −0.9 (it opens on two opposite
  sides); null for every other class.
- `opening_side`: for hole/slot/step/passage/pocket/o_ring/boss/rib/extrude/cut families with a
  significant `net`, the signed dominant axis of net/|net| when that component's magnitude is
  ≥ 0.5; else null.
- `axis` (unit): hole / o_ring / revolve with a cylinder or cone face → the largest such face's
  axis; extrude / cut → the principal (largest-eigenvalue) normal of the `*_end` faces, else the
  direction the planar faces are all perpendicular to; pocket / slot / step / boss / passage with
  a significant `net` → the normal of the floor (largest planar member facing along `net`);
  otherwise the smallest-eigenvalue direction of Σ area·n·nᵀ over planar members (the direction
  all walls are perpendicular to). Signed so axis·net ≥ 0 when `net` is significant, else
  positive along its dominant component. null for fillet, chamfer, rib, stock, other.

## 7. MFCAD (original, 16-class index table)

Source: https://github.com/hducg/MFCAD (MIT), pinned at commit
`ef6d58a40164d5192666821ce98d0cc90e379fac`, `dataset/step/<stem>.step` + `<stem>.face_truth`.
Every STEP `ADVANCED_FACE` is named with a **face id** (0…n−1, not in read order);
`<stem>.face_truth` is a pickled list whose entry `[id]` is that face's class index. Index order
is `FEAT_NAMES` in the repo's `occ/dataset_visualizer.py`. Records use `source.dataset: "mfcad"`,
`part_id: "mfcad/<stem>"`. Every class name also exists in §3.1 with the same meaning and maps
the same way:

| idx | source class | CANON |
|---|---|---|
| 0 | rectangular_through_slot | slot_through |
| 1 | triangular_through_slot | slot_through |
| 2 | rectangular_passage | passage |
| 3 | triangular_passage | passage |
| 4 | 6sides_passage | passage |
| 5 | rectangular_through_step | step_through |
| 6 | 2sides_through_step | step_through |
| 7 | slanted_through_step | step_through |
| 8 | rectangular_blind_step | step_blind |
| 9 | triangular_blind_step | step_blind |
| 10 | rectangular_blind_slot | slot_blind |
| 11 | rectangular_pocket | pocket |
| 12 | triangular_pocket | pocket |
| 13 | 6sides_pocket | pocket |
| 14 | chamfer | chamfer |
| 15 | stock | stock |

The part drops (`LabelError`) when a face name is not an integer id, an id is out of range or
repeated, the label list length differs from the face count, an index is outside 0–15, or the
pickle holds anything but a plain list of ints (class/function references are refused, never
executed). No instance labels: instances are connected same-family faces (§6). The stem's
dash-separated numbers except the last are the generator's feature codes in this same index
space (the last is 19 or 23 in every file, not a class); in all 1,500 parts checked, the labels
were a subset of them (a feature can be consumed by a later one).

## 8. mfcadpp_gen (our port of the MFCAD++ generator)

Source: `training/viewpoint/datasets/mfcadpp_gen/`, an OCP + numpy port of
https://gitlab.com/CAPP-SRC/mfcad2-dataset (MIT, commit
`c171b76bb6f7e2537fcfafe6324b64a07d5e98db`; licence and attribution in the package's `LICENSE`).
Parts are generated locally, one per seed, deterministically (`random.Random(seed)`), with the
upstream's 24 feature classes + stock and its per-face labelling. Records use
`source.dataset: "mfcadpp_gen"`, `part_id: "mfcadpp_gen/<seed>"`, `source.original_id: "<seed>"`,
`source.license` = the generator's MIT notice (the parts themselves are ours).

- Files: `<seed>.step` (AP214; every `ADVANCED_FACE` named with its face id 0…n−1) and
  `<seed>.labels.json` (schema `chudware.viewnet.mfcadpp-gen/1`, `generator_version`
  `mfcadpp_gen/1`): per face id `class` (a §3.1 source class name), `instance` (generator
  feature instance, −1 for stock), `surf`, `area`, `centroid`; plus the drawn `combo` and the
  `applied` features. A seed that yields no valid solid has `<seed>.failed.json` instead and is
  reported as dropped (`LabelError: generation failed: …`).
- CANON: the §3.1 table (same class names, same meaning).
- The reader drops the part (`LabelError`) when a face name is not a known id, an id repeats, the
  sidecar face count differs from the STEP's, a class is not in §3.1, or a face's read-back area
  / centroid disagrees with the sidecar beyond the §5 tolerances (a wrong name → face join). The
  surface type is not compared there (a STEP round trip may re-type a surface — a fillet torus
  reads back as a surface of revolution); §5 matching uses the read-back type.
- Instances: the generator's own (`SourcePart.instances`, §6 first rule), so two touching features
  of one family stay separate features; a face split by a later feature keeps its instance.
- §6 clarification (applies to every source): the floor used for `axis` of a
  pocket/slot/step/boss/passage is the largest planar member whose normal is within 60° of `net`
  (n·net ≥ 0.5 |net|); a side wall leaning only slightly into `net` is not a floor.
