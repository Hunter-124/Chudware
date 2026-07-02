# Slot-fidelity gold set

Small, **human-audited** examples that validate the automatic slot-accuracy metric
(master plan: extraction-based checks missed ~2/3 of number errors when audited — a
human-audited gold set validates the automatic metric itself). Engine-free: each example
is a state dict + captions, so the harness runs in CI without libaicad.

Each `*.json` file is one example object (or a list of them):

```jsonc
{
  "id": "plate_bolt_circle_a",     // stable id (defaults to filename stem)
  "domain": "brep",                // brep | sketch | cam | fea | ecad
  "structure_key": "plate_bolt_circle",  // held-out-STRUCTURES split key
  "state": { ... },                // ground-truth state dict (gsl_ser.enumerate_slots shape)
  "gold_caption": "…",             // OPTIONAL human-written natural caption; the metric
                                   //   must score it ~1.0 (audits the extractor on prose)
  "corruptions": [                 // OPTIONAL; if absent the harness synthesises one
    {"kind": "intrinsic", "caption": "…"},   //   intrinsic + one extrinsic corruption
    {"kind": "extrinsic", "caption": "…"}
  ]
}
```

State sections consumed by `gsl_ser.enumerate_slots` (all optional; a partial state
enumerates only its slots): top-level B-rep (`part`, `bbox_mm`, `faces`, `edges`,
`symmetry`, `feature_groups`, `bores`, `adjacency`) and the domain sections `sketch`,
`cam`, `fea`, `ecad`. See `tools/translator/gsl_ser.py` for the exact keys.

To regenerate the harness baseline after editing gold files:

    python3 -m tools.translator.eval.harness --append-residual
