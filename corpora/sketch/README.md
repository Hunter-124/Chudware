# `corpora/sketch/` — differential-test fixtures (Python oracle ↔ C++ port)

Each `*.json` file here is one sketch-solver fixture. The differential runner
`tools/solver_diff.py` feeds every fixture to **both** engines and diffs the
results:

- the **Python oracle** — `engine.doc.sketch_solver.solve_sketch(sketch, env)`
- the **C++ port** — `core/build/bin/Release/sketch_cli.exe` (calls
  `chudware::sketch::solve_sketch`)

A fixture **passes** when the two engines agree on `converged`, `dof`, the
per-entity `statuses`, the `redundant` / `conflicting` constraint sets, and every
entity coordinate within a tolerance (default `1e-5`).

## Scope of this corpus

These fixtures use **only** the constraint/entity subset currently ported to
C++ (`core/sketch/sketch.cpp`):

- **Entities**: `point`, `line`, `circle`, `arc`.
- **Constraints**: `coincident`, `horizontal`, `vertical`, `parallel`,
  `perpendicular`, `concentric`, `equal`, `midpoint`, `point_on`, `fix`
  (whole-entity + sub-handle), `tangent` (line↔circle), `distance`
  (point-point + point-line), `radius`, `diameter`.

Fixtures requiring deferred features (ellipse/spline/slot entities; `angle`,
`symmetric`, `collinear`, circle-circle tangency; the C50 `cond`/`axis`
directional distances; expression dims beyond a bare env-variable lookup; drag /
warm-start) are intentionally **absent** — they will be added as those features
reach parity. See the runner's report for anything currently deferred.

## Fixture format

```jsonc
{
  "name": "bracket_full",          // required: unique fixture name (== file stem by convention)

  "sketch": {                       // required: the sketch_params dict (doc_schema §3)
    "entities":  [ { "id": "e1", "type": "line", "p1": [0,0], "p2": [80,0] }, ... ],
    "constraints": [ { "id": "c1", "type": "coincident", "refs": ["e1.p2","e2.p1"] }, ... ]
  },

  "env": { "W": 80.0, "H": 50.0 },  // optional: expression-dim variable env (default {})

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

(`construction` and other non-coordinate keys are ignored by the comparator.)

### Constraints

`refs` is a list of handles. A handle is an entity id, optionally with a
sub-handle: `e1`, `e1.p1`, `e1.p2`, `e1.mid`, `e2.center`, `arc.start`,
`arc.end`, or the virtual `origin` / `origin.p`. Dimension constraints
(`distance`, `radius`, `diameter`) carry a `value` object
`{ "value": <number>, "expr": <env-var-name|null> }`; an `expr` that names a key
in `env` overrides the literal value.

### `expect` (optional)

`expect` is **not required** — the harness's primary check is direct Python↔C++
parity, so most fixtures omit it. Add `expect` only for hand-authored
ground-truth cases where you want to additionally pin the expected result
independent of the oracle. When present, the runner asserts the **Python** result
matches `expect` too (coordinates are not checked against `expect`, only
`converged` / `dof` / `statuses` / `redundant` / `conflicting`).

## Running

See `tools/solver_diff.py` (run from the repo root). Quick version:

```powershell
& "$env:APPDATA\Chudware\runtime\venv\Scripts\python.exe" tools/solver_diff.py
```
