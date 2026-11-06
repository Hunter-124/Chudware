# Stress concentration and weak sections

- Inspect the whole load path with `find_stress_risers`, `wall_thickness`, and geometry queries; a geometric screening result is not a strength verdict. Read its locations, sources, confidence, assumptions and incomplete checks.
- Sharp re-entrant corners produce mesh-dependent elastic stress singularities. A single peak or a growing mesh-refinement peak is not a converged material stress. Monitor the same location with `run_fea_convergence`; check fixtures separately.
- Kt is elastic peak stress divided by the specific chart's nominal stress; it is not Kf, factor of safety, or a universal multiplier. Use `research_eng_calc` stress_concentration only when the actual geometry, load and validity ranges match. For fatigue, avoid applying a notch factor twice to a resolved notch field.
- Prefer load-path reinforcement, thicker sections, or generous smooth transitions that preserve fit and function. For printing, exploit curved load paths and consider build orientation and anisotropy. For machining, check cutter access, radius, material removal, cost and required clearances.
- Dry-run proposed features and inspect the effective radius (the engine can clamp it), then rebuild and evaluate the altered geometry and load case. Do not claim a successful strength fix from a successful build alone.
