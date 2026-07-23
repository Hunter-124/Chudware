# Thin wall context pack
# Source: services/research/calcs/wall_thickness.py;
#         engine_oracle/sim/materials.json
#         Reference: Machinery's Handbook 31st ed. §Plastics Mold Design;
#                    manufacturer DFM guides (Xometry, Protolabs)

## Minimum wall thickness by process (wall_thickness.py _PROCESS_DEFAULTS)
- FDM / FFF:           min 1.2 mm  (2–3 perimeters at 0.4 mm nozzle)
- SLA / MSLA:          min 0.6 mm  (unsupported); 1.0 mm (structural)
- Injection moulding:  min 0.8 mm  (thermoplastic); flow-length / t ≤ 200:1
- CNC machining:       min 0.75 mm metal; 1.5 mm plastic (tool-deflection limited)
- Sheet metal bend:    min = 1× material thickness beside bend

## 2 mm heuristic trigger
- Wall < 2 mm in any structural plastic feature → check DFM for process
- Wall < 1 mm in metal → confirm machining feasibility with cam_simulate

## Callout note on drawing
"MIN WALL 1.5 [±0.1] — SEE DFM NOTE" (always specify tolerance, not just nominal)

## Prerun calc: wall_thickness — answer.min_wall_mm, answer.process_limit_mm
