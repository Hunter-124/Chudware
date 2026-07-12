# simsuite — unified simulation benchmark harness

Every solver family contributes one bench module: `benchmarks/simsuite/<domain>_bench.py`.

## Contract

Each bench module exposes:

```python
def run(quick: bool = False) -> list[dict]
```

returning one dict per benchmark case:

```python
{
  "domain": "fea_static",        # solver family key
  "case": "cantilever_tet10",    # unique case id within the domain
  "setup_s": 0.12,               # geometry/doc build time
  "mesh_s": 1.40,                # meshing time (0.0 if N/A)
  "solve_s": 3.90,               # solver time
  "total_s": 5.42,               # wall clock for the whole case
  "metric": 1.02e-3,             # the measured quantity (deflection, freq, Sxx, ...)
  "ref": 1.00e-3,                # analytic / reference value (None if regression-only)
  "rel_err": 0.02,               # abs(metric-ref)/abs(ref) (None if no ref)
  "status": "ok",                # ok | fail | skip
  "detail": "",                  # free text (skip reason, failure message, units)
}
```

Rules:
- `quick=True` must finish the whole module in under ~60 s (coarse meshes) —
  used for smoke runs. Full mode is the timing baseline.
- Run the engine **in-process** (`AI_CAD_ENGINE_DAEMON=0`, services/dist lib via
  `services.mcp.core_bridge`) — never against a live daemon/GUI.
- No load-bearing constants from memory: analytic references carry a comment
  citing the formula (Euler-Bernoulli, Kirsch, Lamé, ...).
- Timing collected while other processes hammer the box is smoke-only; the
  authoritative baseline is a serial `run_all.py` pass.

## Running

```bash
.venv-mcp/bin/python benchmarks/simsuite/run_all.py            # full baseline
.venv-mcp/bin/python benchmarks/simsuite/run_all.py --quick    # smoke
.venv-mcp/bin/python benchmarks/simsuite/run_all.py --only fea_static,em
```

Results land in `benchmarks/simsuite/results/<UTC-timestamp>/results.json` plus
`REPORT.md` (accuracy + timing tables, slowest cases first).
