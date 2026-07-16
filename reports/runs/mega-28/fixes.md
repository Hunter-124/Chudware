# mega-28 RouterFix bind (E28)

**Status:** router proven — fixes bound
**RouterFix:** FatalGrouse
**Baseline:** 271aedac
**NEW hostd stamp:** night2-3084-g271aedac-dirty
**md5:** 6b9c8502383cd964bfbfe38a5946728b

## E28 — C++ / Python autorouter min clearance

Deterministic mega-27-class board replay:
- BEFORE: clearance_errors=6 (chamfer 0.5, pitch 0.25)
- AFTER: clearance_errors=0 (chamfer 0, min_clearance=0.2)
- AFTER pitch=0.3 ladder: clearance_errors=0
- C++ libaicad: clearance_errors=0

Proof: `reports/runs/mega-28/router_proof.json` + `router_proof.md` (verdict PASS).

Root cause: Python corner chamfer (0.5mm default) produced pad↔track sub-clearance;
C++ `clearance_of()` + opts `min_clearance`/`min_clearance_mm` floored; MCP/agent plumb.

DRC 0.2mm rule held (not weakened).

## F28 readiness

Harness ready for full mission 9/9 after this bind. Launch via `launch_primary.sh`.

router proven. E28 bound. Fixes bound.
