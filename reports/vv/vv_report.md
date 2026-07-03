# V&V Benchmark Report — AI-CAD Simulation Backends

**Generated:** 2026-07-03T10:22:05Z  
**Script:** `scripts/vv_benchmark.py` (T-BL01)  
**Methodology:** Independent analytic reference solutions (textbook closed-form); no reference values read from the engine under test.  
**NAFEMS/ASME documents:** NOT bundled. Target values hand-typed with inline citation.  

## Summary

| Metric | Count |
|--------|-------|
| Executed cases | 16 |
| PASS | 16 |
| FAIL | 0 |
| TREND-PASS | 0 |
| SKIP (env/dep) | 4 |
| Elapsed | 0.2s |

## Tolerance Tiers

| Tier | Threshold | Meaning |
|------|-----------|---------|
| EXACT | < 0.01% | Same closed-form — should be machine-exact |
| PRECISE | < 1.0% | Validated analytic reference |
| ENGINEERING | < 10.0% | Engineering-grade estimate |
| TREND | directional | Physical trend correct (labeled explicitly) |

## Family: hydraulic

| Case | Tier | Verdict | Got | Ref | Err% | Citation |
|------|------|---------|-----|-----|------|----------|
| H1-retract-force | EXACT | **PASS** | 46.56 | 46.56 | 0.000% | Merritt 1967 §2.2: F_ret = (A_bore-A_rod)*P*η |
| H2-pump-displacement | EXACT | **PASS** | 30 | 30 | 0.000% | Vickers Ind. Hyd. Manual 1992 ch.3: disp=Q*1000/N |
| H2-pump-shaft-power | EXACT | **PASS** | 10.23 | 10.23 | 0.000% | Vickers Ind. Hyd. Manual 1992 ch.3: P_shaft=Q*dP/η |

## Family: piping

| Case | Tier | Verdict | Got | Ref | Err% | Citation |
|------|------|---------|-----|-----|------|----------|
| P1-straight-pipe | ENGINEERING | **PASS** | 2.803 | 2.739 | 2.330% | White 2011 §8.4: h_f=f(L/D)(v²/2g), Blasius f |
| P2-length-scaling | ENGINEERING | **PASS** | 0.6892 | 0.7071 | 2.527% | Darcy-Weisbach: Q∝√(dH/L) → Q(2L)/Q(L)=1/√2 |

## Family: ecad-impedance

| Case | Tier | Verdict | Got | Ref | Err% | Citation |
|------|------|---------|-----|-----|------|----------|
| I1-microstrip-narrow | EXACT | **PASS** | 110.6 | 110.6 | 0.000% | IPC-2141A §6.2: Z0=87/√(εr+1.41)×ln(5.98h/(0.8w+t)) |
| I2-microstrip-wide | EXACT | **PASS** | 48.97 | 48.97 | 0.000% | IPC-2141A §6.2 |
| I3-stripline | EXACT | **PASS** | 37.15 | 37.15 | 0.000% | IPC-2141A §6.3: Z0=60/√εr×ln(4h/(0.67π(0.8w+t))) |
| I4-diff-pair | EXACT | **PASS** | 127 | 127 | 0.000% | IPC-2141A §6.4: Z_diff=2·Z0_se·(1-0.48·exp(-0.96·s/h)) |

## Family: ecad-circuit

| Case | Tier | Verdict | Got | Ref | Err% | Citation |
|------|------|---------|-----|-----|------|----------|
| C1-RC-lowpass-corner | PRECISE | **PASS** | 0.7071 | 0.7071 | 0.000% | Hayt&Kemmerly 8th §14.5: |H(jωc)|=1/√2 at f=1/(2πRC) |
| C2-RLC-resonance-frequency | ENGINEERING | **PASS** | 5012 | 5033 | 0.418% | Sedra&Smith 6th §1.5: ω0=1/√(LC), series RLC peak at ω0 |
| C3-thevenin-load | EXACT | **PASS** | 6.545 | 6.545 | 0.000% | Hayt&Kemmerly 8th §5.3: V_load=V_th*R_L/(R_th+R_L) |

## Family: structural

| Case | Tier | Verdict | Got | Ref | Err% | Citation |
|------|------|---------|-----|-----|------|----------|
| S1-cantilever-tip-deflection | PRECISE | **PASS** | 6.75 | 6.75 | 0.000% | Roark 8th Table 8.1 Case 1a: δ=PL³/(3EI) |
| S2-simply-supported-UDL | PRECISE | **PASS** | 2.531 | 2.531 | 0.000% | Roark 8th Table 8.1 Case 2a: δ=5wL⁴/(384EI) |
| S3-fixed-fixed-midspan | PRECISE | **PASS** | 0.6104 | 0.6104 | 0.000% | Roark 8th Table 8.1 Case 3b: δ=PL³/(192EI) |
| S4-two-span-continuous-UDL | ENGINEERING | **PASS** | 0.03244 | 0.0322 | 0.747% | Hibbeler 9th §12.1 + double integration: δ_max≈0.005301wL⁴/( |

## Family: modal

| Case | Tier | Verdict | Got | Ref | Err% | Citation |
|------|------|---------|-----|-----|------|----------|
| M1-cantilever-first-mode *(skipped)* | N/A | **SKIP** | — | 40.77 | — | Rao 5th §8.2 Table 8.1: β₁L=1.87510 for cantilever |
| M2-simply-supported-first-mode *(skipped)* | N/A | **SKIP** | — | 114.4 | — | Rao 5th §8.2: β₁L=π for SS-SS |

## Family: fea-static

| Case | Tier | Verdict | Got | Ref | Err% | Citation |
|------|------|---------|-----|-----|------|----------|
| FEA1-cantilever-deflection *(skipped)* | N/A | **SKIP** | — | — | — | Roark 8th Table 8.1 Case 1a: δ=PL³/(3EI) |

## Family: cfd-lbm

| Case | Tier | Verdict | Got | Ref | Err% | Citation |
|------|------|---------|-----|-----|------|----------|
| LBM-T1-drag-velocity-squared *(skipped)* | N/A | **SKIP** | — | — | — | Drag equation: F=½ρCdAV² → Cd=const for fixed Re regime |

## Reference Sources

All reference formulas are public-domain textbook physics. No NAFEMS or ASME 
documents are bundled; target values are derived from first principles or 
hand-typed single numbers with inline citation.

| Source | Used in |
|--------|---------|
| Roark 8th ed Table 8.1-8.2 | S1, S2, S3, S4, FEA1 |
| Hibbeler *Structural Analysis* 9th §12.1 | S4 |
| Hayt & Kemmerly *Engineering Circuit Analysis* 8th | C1, C3 |
| Sedra & Smith *Microelectronic Circuits* 6th §1.5 | C2 |
| IPC-2141A §6.2-6.4 (public-domain PCB standard) | I1-I4 |
| White *Fluid Mechanics* 8th §8.4 | P1, P2 |
| Merritt *Hydraulic Control Systems* (1967) §2.2 | H1 |
| Vickers Industrial Hydraulics Manual (1992) ch.3 | H2 |
| Rao *Mechanical Vibrations* 5th §8.2 | M1, M2 |
| Anderson *Introduction to Flight* 8th §5.4 | LBM-T1 |

## CI Integration

```bash
# Smoke subset (pure-Python, no .so required):
pytest scripts/vv_benchmark.py -k 'smoke' -q

# Full suite:
AICAD_RUN_VV=full AI_CAD_LIBAICAD=/path/to/libaicad.so \
    pytest scripts/vv_benchmark.py -q
```

Gate integration: `gate.sh` stage 4.5 runs the smoke subset when `AICAD_RUN_VV` is set (skipped by default, same pattern as `AICAD_RUN_BENCH`).