# gsl_dataset — synthetic GSL corpus (labels for free)

Procedurally generated parametric parts with per-face supervision, per
`docs/GEOMETRY_LLM_REPRESENTATION_REVIEW.md` §3.4 ("synthetic-label factory":
the feature op that created a face labels that face — zero hand-labeling).

Samples are **regenerable artifacts and are NOT committed** (see `.gitignore`).

## Regenerate

```
python3 tools/gsl_dataset_gen.py --count 50            # 50 samples per family
python3 tools/gsl_dataset_gen.py --count 5 --families plate_bolt_circle,turned_shaft
python3 tools/gsl_dataset_gen.py --count 5 --render    # + render.png per sample
```

Run from the repo root; requires the built C++ core (`libaicad.so` under
`core/build-linux/bin` or `core/build-occt-linux/bin`, resolved by
`mcp_server/core_bridge.py`).

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
joining `doc_tree`'s `owner_map` `[body_id, topo_id]` pairs against the match's
`hint_index+1` (both follow the same `describe_faces()` enumeration order —
`doc_query`'s own `owner_feature` field is always null on this RPC path).
Ownerless faces get `"import_or_base"`.

## Consumers

- `tools/gsl_eval.py` — recognition measured-residual harness
  (`docs/RECOGNITION_RESIDUAL.md`).
- `tools/gsl_gnn/` — gated GNN adapter scaffold (train only where the residual
  report justifies it).
