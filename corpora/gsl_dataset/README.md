# gsl_dataset — synthetic GSL corpus (labels for free)

Procedurally generated parametric parts with per-face supervision: the feature op
that created a face labels that face — zero hand-labeling.

Samples are **regenerable artifacts and are NOT committed** (see `.gitignore`).

## Regenerate

```
python3 -m tools.gsl_dataset_gen --count 50            # 50 samples per family
python3 -m tools.gsl_dataset_gen --count 5 --families plate_bolt_circle,turned_shaft
python3 -m tools.gsl_dataset_gen --count 5 --render    # + render.png per sample
```

Run from the repo root; requires the built C++ core (`libchudware.so` under
`core/build-linux/bin` or `core/build-occt-linux/bin`, resolved by
`services/mcp/core_bridge.py`).

## Families (7)

`plate_bolt_circle`, `pocketed_block`, `turned_shaft` (revolve),
`ribbed_bracket`, `shelled_enclosure`, `bladed_disk` (circular pattern),
`swept_pipe` — each a template `(rng) -> ops list` with randomized dims.

## Layout

```
<family>/<seed>/
  document.json    doc_tree result (full feature tree + owner_map)
  faces.json       doc_query {of:"face"} matches + per-face `label`
  adjacency.json   doc_adjacency result (face graph, convex/concave edges)
  outline.txt      describe_part text (gsl_tools._outline_text(_digest(...)))
  meta.json        family, seed, ops/feature counts, bbox, volume, version
  render.png       only with --render
```

## Label semantics

`faces.json[i].label` = the TYPE of the feature that owns the face, resolved by
joining `doc_tree`'s `owner_map` `[body, face_id]` pairs (body ordinal, per-body
1-based TopExp face id) against the match's engine-stamped `body` and `topo`
keys. `doc_query` numbers faces across EVERY body with one running
`hint_index`, so `hint_index+1` equals the per-body id only on the first body;
joining on it (as the generator did before 2026-09-28) mislabels every
multi-body sample. `doc_query`'s own `owner_feature` field is always null on
this RPC path. Ownerless faces get `"import_or_base"`.

`adjacency.json` comes from `doc_adjacency`, which covers the LAST body only:
its `ord` is that body's describe_faces index, so on a multi-body sample
`faces.json` index = `ord` + the face count of every preceding body.

## Consumers

- `tools/gsl_gnn/train_ranker.py` — retrains the candidate-ranker weights
  (`tools/gsl_gnn/ranker_weights.json`) from `faces.json`.
