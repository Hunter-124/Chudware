# `corpora/sketch/` — sketch-solver fixture corpus

Each `*.json` file here is one sketch-solver fixture: a sketch, its expression
environment, and (for most fixtures) hand-authored expected results.

## Legacy oracle corpus

The corpus was built as the differential corpus for porting the sketch solver
from the Python oracle (`engine.doc.sketch_solver`) to C++. Its runner
(`tools/solver_diff.py`) fed every fixture to both engines and required
agreement on `converged`, `dof`, per-entity `statuses`, the `redundant` /
`conflicting` sets, and entity coordinates within `1e-5`. The oracle and that
runner have been deleted, so no automated job iterates this directory today;
the fixtures remain the recorded parity cases for the native solver.

## Native tooling

The live solver is `chudware::sketch::solve_sketch` (`core/sketch/`). Its
standalone driver `sketch_cli` (`core/sketch/sketch_cli.cpp`, CMake target
`sketch_cli`, emitted to `<build>/bin/`) solves any fixture in this directory
directly, ignoring the extra `name` / `expect` / `_note` fields:

```bash
core/build-linux/bin/sketch_cli corpora/sketch/bracket_full.json
```

It prints one JSON line `{converged, dof, redundant, conflicting, statuses,
entities}`. Compare that against the fixture's `expect` block by hand; there is
no automated corpus runner for this directory. The SolidWorks-parity cases in
`corpora/sw_parity/` have their own runner over the same binary
(`corpora/sw_parity/_run_fixtures.py`). The C++ unit tests (`test_sketch` and
the related `core/tests/` suites) carry their own inline cases.

New sketch capability follows `core/README.md`: implement the residual and
analytic Jacobian in `core/sketch/`, add a fixture here, and add or update the
C++ solver test.

## Coverage

The corpus spans `point`, `line`, `circle`, `arc`, and the ported `slot`,
`arc_slot`, `ellipse`, `ellipse_arc`, `conic`, `parabola` and `spline`
entities, with `coincident`, `horizontal`, `vertical`, `parallel`,
`perpendicular`, `collinear`, `concentric`, `coradial`, `equal`, `midpoint`,
`symmetric`, `point_on`, `fix` (whole-entity + sub-handle), `tangent`,
`angle`, `distance`, `radius` and `diameter` constraints, plus expression
dimensions driven by `env`.

## Fixture format

```jsonc
{
  "name": "bracket_full",          // required: unique fixture name (== file stem by convention)

  "sketch": {                       // required: the sketch_params dict (doc_schema §3)
    "entities":  [ { "id": "e1", "type": "line", "p1": [0,0], "p2": [80,0] }, ... ],
    "constraints": [ { "id": "c1", "type": "coincident", "refs": ["e1.p2","e2.p1"] }, ... ]
  },

  "env": { "W": 80.0, "H": 50.0 },  // expression-dim variable env ({} when unused)

  "expect": {                       // optional: hand-authored ground truth (see below)
    "converged": true,
    "dof": 0,
    "statuses": { "e1": "full", ... },
    "redundant":   [],
    "conflicting": []
  }
}
```

### Entity shapes (input and solved output)

| type     | coordinate keys                                  |
|----------|--------------------------------------------------|
| `point`  | `p: [x, y]`                                       |
| `line`   | `p1: [x, y]`, `p2: [x, y]`                         |
| `circle` | `center: [x, y]`, `radius: r`                     |
| `arc`    | `center: [x, y]`, `radius: r`, `start: deg`, `end: deg` |

The ported curve entities carry their own parameter keys; see the matching
`ported_*.json` fixture and `core/sketch/` for each shape. `construction` and
other non-coordinate keys are not solver coordinates.

### Constraints

`refs` is a list of handles. A handle is an entity id, optionally with a
sub-handle: `e1`, `e1.p1`, `e1.p2`, `e1.mid`, `e2.center`, `arc.start`,
`arc.end`, or the virtual `origin` / `origin.p`. Dimension constraints
(`distance`, `radius`, `diameter`, `angle`) carry a `value` object
`{ "value": <number>, "expr": <expression|null> }`; an `expr` evaluated against
`env` overrides the literal value.

### `expect` (optional)

`expect` pins hand-authored ground truth: `converged`, `dof`, `statuses`,
`redundant`, `conflicting`. Coordinates are not part of `expect`; under the
legacy runner they were checked by engine-to-engine agreement only.
